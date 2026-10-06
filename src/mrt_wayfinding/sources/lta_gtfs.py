"""Strict collection and parsing of LTA's GTFS Schedule (Train) feed."""
from collections import defaultdict
from collections.abc import Callable, Mapping
import csv
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from io import BytesIO, StringIO
import json
import math
from pathlib import Path
from pathlib import PurePosixPath
import re
import time
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile

from .. import __version__
from ..contracts import ContractError
from ..station_catalog import STATION_CODE, StationCatalog, station_key
from ..storage import OutputExistsError, atomic_write_bytes, atomic_write_json


class GtfsSourceError(ContractError):
    """Raised when GTFS transport or content violates the source contract."""


def require(condition, message):
    if not condition:
        raise GtfsSourceError(message)


def fields(value, expected, context):
    require(type(value) is dict, f"{context}: expected object")
    actual, expected = set(value), set(expected)
    require(
        actual == expected,
        f"{context}: unexpected fields; missing={sorted(expected-actual)}, "
        f"extra={sorted(actual-expected)}",
    )


def valid_code(code):
    return type(code) is str and STATION_CODE.fullmatch(code) is not None


def digest(raw):
    return sha256(raw).hexdigest()


REQUIRED_FILES = {
    "agency.txt", "routes.txt", "trips.txt", "stops.txt", "stop_times.txt",
    "calendar.txt", "calendar_dates.txt", "feed_info.txt",
}
MAX_ZIP_BYTES = 100 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
MAX_MEMBER_BYTES = 250 * 1024 * 1024
CODE_PATTERN = re.compile(r"(?<![A-Z0-9])(?:[A-Z]{1,3}[0-9]+|STC|PTC)(?![A-Z0-9])")

SCHEMAS = {
    "feed_info.txt": (
        {
            "feed_publisher_name", "feed_publisher_url", "feed_lang",
            "feed_start_date", "feed_end_date", "feed_version",
        },
        {
            "feed_publisher_name", "feed_publisher_url", "feed_lang",
            "feed_start_date", "feed_end_date", "feed_version",
        },
    ),
    "agency.txt": ({"agency_name", "agency_url", "agency_timezone"},
                   {"agency_id", "agency_name", "agency_url", "agency_timezone",
                    "agency_lang", "agency_phone", "agency_fare_url", "agency_email"}),
    "routes.txt": ({"route_id", "route_type"},
                   {"route_id", "agency_id", "route_short_name", "route_long_name",
                    "route_desc", "route_type", "route_url", "route_color",
                    "route_text_color", "route_sort_order", "continuous_pickup",
                    "continuous_drop_off", "network_id"}),
    "trips.txt": ({"route_id", "service_id", "trip_id"},
                  {"route_id", "service_id", "trip_id", "trip_headsign",
                   "trip_short_name", "direction_id", "block_id", "shape_id",
                   "wheelchair_accessible", "bikes_allowed"}),
    "stops.txt": ({"stop_id", "stop_name", "stop_lat", "stop_lon"},
                  {"stop_id", "stop_code", "stop_name", "tts_stop_name", "stop_desc",
                   "stop_lat", "stop_lon", "zone_id", "stop_url", "location_type",
                   "parent_station", "stop_timezone", "wheelchair_boarding", "level_id",
                   "platform_code"}),
    "stop_times.txt": ({"trip_id", "stop_id", "stop_sequence"},
                       {"trip_id", "arrival_time", "departure_time", "stop_id",
                        "location_group_id", "location_id", "stop_sequence", "stop_headsign",
                        "start_pickup_drop_off_window", "end_pickup_drop_off_window",
                        "pickup_type", "drop_off_type", "continuous_pickup",
                        "continuous_drop_off", "shape_dist_traveled", "timepoint",
                        "pickup_booking_rule_id", "drop_off_booking_rule_id"}),
    "calendar.txt": ({"service_id", "monday", "tuesday", "wednesday", "thursday",
                      "friday", "saturday", "sunday", "start_date", "end_date"},
                     {"service_id", "monday", "tuesday", "wednesday", "thursday",
                      "friday", "saturday", "sunday", "start_date", "end_date"}),
    "calendar_dates.txt": ({"service_id", "date", "exception_type"},
                           {"service_id", "date", "exception_type"}),
}


def _rows(raw, filename):
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{filename}: must be UTF-8") from exc
    require("\x00" not in text, f"{filename}: contains NUL")
    reader = csv.DictReader(StringIO(text, newline=""))
    require(reader.fieldnames is not None and all(reader.fieldnames),
            f"{filename}: missing/blank header")
    require(len(reader.fieldnames) == len(set(reader.fieldnames)),
            f"{filename}: duplicate header")
    required, allowed = SCHEMAS[filename]
    actual = set(reader.fieldnames)
    require(required <= actual, f"{filename}: missing fields {sorted(required - actual)}")
    require(actual <= allowed, f"{filename}: unexpected fields {sorted(actual - allowed)}")
    result = []
    for number, row in enumerate(reader, start=2):
        require(None not in row and all(value is not None for value in row.values()),
                f"{filename}:{number}: wrong column count")
        require(any(value.strip() for value in row.values()),
                f"{filename}:{number}: blank row")
        result.append({key: value.strip() for key, value in row.items()})
    require(result or filename == "calendar_dates.txt", f"{filename}: empty table")
    return result


def _unique(rows, key, filename):
    result = {}
    for number, row in enumerate(rows, start=2):
        value = row[key]
        require(value and value not in result, f"{filename}:{number}: blank/repeated {key}")
        result[value] = row
    return result


def _gtfs_date(value, context):
    require(bool(re.fullmatch(r"[0-9]{8}", value)), f"{context}: expected YYYYMMDD")
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:]))
    except ValueError as exc:
        raise ValueError(f"{context}: invalid date") from exc


def _active_services(calendar_rows, exception_rows, service_date):
    weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    calendars = _unique(calendar_rows, "service_id", "calendar.txt")
    active = set()
    for service_id, row in calendars.items():
        require(all(row[day] in {"0", "1"} for day in weekdays),
                f"calendar.txt {service_id}: invalid weekday flag")
        start = _gtfs_date(row["start_date"], f"calendar.txt {service_id}.start_date")
        end = _gtfs_date(row["end_date"], f"calendar.txt {service_id}.end_date")
        require(start <= end, f"calendar.txt {service_id}: inverted date range")
        if start <= service_date <= end and row[weekdays[service_date.weekday()]] == "1":
            active.add(service_id)
    seen_exceptions = set()
    for number, row in enumerate(exception_rows, start=2):
        when = _gtfs_date(row["date"], f"calendar_dates.txt:{number}.date")
        require(row["exception_type"] in {"1", "2"},
                f"calendar_dates.txt:{number}: invalid exception_type")
        identity = (row["service_id"], when)
        require(identity not in seen_exceptions,
                f"calendar_dates.txt:{number}: duplicate service/date exception")
        seen_exceptions.add(identity)
        if when == service_date:
            if row["exception_type"] == "1":
                active.add(row["service_id"])
            else:
                active.discard(row["service_id"])
    require(active, f"No GTFS service is active on {service_date.isoformat()}")
    return active


def _codes(*values):
    found = []
    for value in values:
        for code in CODE_PATTERN.findall(value.upper()):
            if valid_code(code) and code not in found:
                found.append(code)
    # Child stops are accumulated in sets, so their traversal order varies
    # with Python's hash seed. Canonicalize the extracted codes before they
    # enter a persisted snapshot.
    return sorted(found)


def _feed_information(rows, service_date):
    require(len(rows) == 1, f"feed_info.txt: expected exactly one row, got {len(rows)}")
    row = rows[0]
    require(row["feed_publisher_name"] == "Land Transport Authority",
            "feed_info.txt: unexpected feed_publisher_name")
    publisher = urlsplit(row["feed_publisher_url"])
    require(
        publisher.scheme == "https"
        and publisher.hostname in {"lta.gov.sg", "www.lta.gov.sg"}
        and publisher.username is None
        and publisher.password is None
        and publisher.port in (None, 443)
        and not publisher.query
        and not publisher.fragment,
        "feed_info.txt: unexpected feed_publisher_url",
    )
    require(row["feed_lang"] == "en", "feed_info.txt: expected feed_lang 'en'")
    start = _gtfs_date(row["feed_start_date"], "feed_info.txt.feed_start_date")
    end = _gtfs_date(row["feed_end_date"], "feed_info.txt.feed_end_date")
    require(start <= end, "feed_info.txt: inverted date range")
    require(start <= service_date <= end,
            f"feed_info.txt: requested service date {service_date.isoformat()} is outside "
            f"the published feed range {start.isoformat()}..{end.isoformat()}")
    version = row["feed_version"]
    require(bool(version) and len(version) <= 64, "feed_info.txt: invalid feed_version")
    return {
        "publisher_name": row["feed_publisher_name"],
        "publisher_url": row["feed_publisher_url"],
        "language": row["feed_lang"],
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "version": version,
    }


def _validate_normalized_feed_information(value, service_date, context):
    """Validate the normalized feed metadata again when loading a snapshot."""

    fields(
        value,
        {"publisher_name", "publisher_url", "language", "start_date", "end_date", "version"},
        context,
    )
    require(value["publisher_name"] == "Land Transport Authority",
            f"{context}.publisher_name: unexpected value")
    require(type(value["publisher_url"]) is str, f"{context}.publisher_url: expected string")
    publisher = urlsplit(value["publisher_url"])
    require(
        publisher.scheme == "https"
        and publisher.hostname in {"lta.gov.sg", "www.lta.gov.sg"}
        and publisher.username is None
        and publisher.password is None
        and publisher.port in (None, 443)
        and not publisher.query
        and not publisher.fragment,
        f"{context}.publisher_url: unexpected URL",
    )
    require(value["language"] == "en", f"{context}.language: expected 'en'")
    for key in ("start_date", "end_date"):
        require(
            type(value[key]) is str
            and bool(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value[key])),
            f"{context}.{key}: expected YYYY-MM-DD",
        )
    start = _gtfs_date(value["start_date"].replace("-", ""), f"{context}.start_date")
    end = _gtfs_date(value["end_date"].replace("-", ""), f"{context}.end_date")
    require(start <= end, f"{context}: inverted date range")
    require(
        start <= service_date <= end,
        f"{context}: service date {service_date.isoformat()} is outside "
        f"{start.isoformat()}..{end.isoformat()}",
    )
    require(
        type(value["version"]) is str and bool(value["version"]) and len(value["version"]) <= 64,
        f"{context}.version: expected nonempty string of at most 64 characters",
    )


def active_inventory(zip_bytes, service_date):
    """Return stations served by at least one scheduled trip on service_date."""
    require(type(zip_bytes) is bytes and 0 < len(zip_bytes) <= MAX_ZIP_BYTES,
            "GTFS ZIP is empty or oversized")
    try:
        archive = ZipFile(BytesIO(zip_bytes))
    except BadZipFile as exc:
        raise ValueError("LTA GTFS response is not a valid ZIP") from exc
    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        require(len(names) == len(set(names)), "GTFS ZIP contains repeated member names")
        require(set(names) == REQUIRED_FILES,
                f"GTFS ZIP members changed; missing={sorted(REQUIRED_FILES-set(names))}, "
                f"extra={sorted(set(names)-REQUIRED_FILES)}")
        total = 0
        tables = {}
        for info in infos:
            path = PurePosixPath(info.filename)
            mode = info.external_attr >> 16
            require(len(path.parts) == 1 and path.name == info.filename
                    and not info.is_dir() and (mode & 0o170000) != 0o120000,
                    f"GTFS ZIP has unsafe member: {info.filename}")
            require(0 < info.file_size <= MAX_MEMBER_BYTES,
                    f"GTFS ZIP member empty/oversized: {info.filename}")
            total += info.file_size
            require(total <= MAX_UNCOMPRESSED_BYTES, "GTFS ZIP expands beyond size limit")
            raw = archive.read(info)
            require(len(raw) == info.file_size, f"GTFS ZIP member truncated: {info.filename}")
            tables[info.filename] = _rows(raw, info.filename)

    stops = _unique(tables["stops.txt"], "stop_id", "stops.txt")
    feed_information = _feed_information(tables["feed_info.txt"], service_date)
    routes = _unique(tables["routes.txt"], "route_id", "routes.txt")
    trips = _unique(tables["trips.txt"], "trip_id", "trips.txt")
    active_services = _active_services(tables["calendar.txt"], tables["calendar_dates.txt"], service_date)
    active_trips = {trip_id: row for trip_id, row in trips.items() if row["service_id"] in active_services}
    require(active_trips, f"No GTFS trips are active on {service_date.isoformat()}")
    known_services = ({row["service_id"] for row in tables["calendar.txt"]}
                      | {row["service_id"] for row in tables["calendar_dates.txt"]})
    for row in trips.values():
        require(row["route_id"] in routes, f"trips.txt: unknown route_id {row['route_id']}")
        require(row["service_id"] in known_services,
                f"trips.txt: unknown service_id {row['service_id']}")

    station_children = defaultdict(set)
    station_routes = defaultdict(set)
    sequences = set()
    active_trip_seen = set()
    for number, row in enumerate(tables["stop_times.txt"], start=2):
        require(row["trip_id"] in trips, f"stop_times.txt:{number}: unknown trip_id")
        require(row["stop_id"] in stops, f"stop_times.txt:{number}: unknown stop_id")
        require(row["stop_sequence"].isdigit(), f"stop_times.txt:{number}: invalid stop_sequence")
        identity = (row["trip_id"], int(row["stop_sequence"]))
        require(identity not in sequences, f"stop_times.txt:{number}: repeated trip/sequence")
        sequences.add(identity)
        if row["trip_id"] not in active_trips:
            continue
        active_trip_seen.add(row["trip_id"])
        child = stops[row["stop_id"]]
        parent_id = child.get("parent_station", "") or row["stop_id"]
        require(parent_id in stops, f"stops.txt {row['stop_id']}: unknown parent_station")
        station_children[parent_id].add(row["stop_id"])
        station_routes[parent_id].add(active_trips[row["trip_id"]]["route_id"])
    require(active_trip_seen == set(active_trips),
            f"Active trips missing stop_times: {sorted(set(active_trips)-active_trip_seen)[:20]}")

    result = []
    for station_id in sorted(station_children):
        station = stops[station_id]
        location_type = station.get("location_type", "")
        require(location_type in {"", "0", "1"},
                f"Active station {station_id}: unsupported location_type {location_type!r}")
        route_ids = sorted(station_routes[station_id])
        route_names = []
        for route_id in route_ids:
            route = routes[route_id]
            name = route.get("route_short_name", "") or route.get("route_long_name", "")
            if name and name not in route_names:
                route_names.append(name)
        codes = _codes(station.get("stop_code", ""), station["stop_name"], station_id,
                       *(stops[child].get("stop_code", "") for child in station_children[station_id]),
                       *(stops[child]["stop_name"] for child in station_children[station_id]))
        result.append({"stop_id": station_id, "stop_code": station.get("stop_code", ""),
                       "name": station["stop_name"], "codes": codes,
                       "child_stop_ids": sorted(station_children[station_id]),
                       "route_ids": route_ids, "route_names": route_names})
    require(result, "No active GTFS stations were derived")
    return {"schema_version": 1, "status": "complete", "kind": "lta_gtfs_active_train_stations",
            "service_date": service_date.isoformat(), "source_zip_sha256": digest(zip_bytes),
            "feed_info": feed_information,
            "active_service_count": len(active_services), "active_trip_count": len(active_trips),
            "active_station_count": len(result), "stations": result}


def match_stage1_stations(stage1, inventory):
    """Map every active LTA GTFS station to exactly one SgWiki semantic station."""
    fields(inventory, {"schema_version", "status", "kind", "service_date", "feed_info",
                       "source_zip_sha256", "active_service_count", "active_trip_count",
                       "active_station_count", "stations"}, "GTFS active inventory")
    require(inventory["schema_version"] == 1 and inventory["status"] == "complete"
            and inventory["kind"] == "lta_gtfs_active_train_stations",
            "Unsupported/incomplete GTFS active inventory")
    require(type(inventory["service_date"]) is str
            and bool(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", inventory["service_date"])),
            "GTFS service_date: expected YYYY-MM-DD")
    service_date = _gtfs_date(
        inventory["service_date"].replace("-", ""), "GTFS service_date"
    )
    _validate_normalized_feed_information(
        inventory["feed_info"], service_date, "GTFS active inventory.feed_info"
    )
    require(type(inventory["source_zip_sha256"]) is str
            and bool(re.fullmatch(r"[0-9a-f]{64}", inventory["source_zip_sha256"])),
            "GTFS active inventory: invalid source ZIP hash")
    for key in ("active_service_count", "active_trip_count", "active_station_count"):
        require(type(inventory[key]) is int and inventory[key] > 0,
                f"GTFS active inventory.{key}: expected positive integer")
    require(type(inventory["stations"]) is list
            and inventory["active_station_count"] == len(inventory["stations"]),
            "GTFS active station count disagrees with rows")
    by_id = {station["id"]: station for station in stage1["stations"]}
    require(len(by_id) == len(stage1["stations"]), "Stage 1 has repeated station IDs")
    matches = defaultdict(list)
    seen_stops = set()
    for index, row in enumerate(inventory["stations"]):
        context = f"GTFS active stations[{index}]"
        fields(row, {"stop_id", "stop_code", "name", "codes", "child_stop_ids",
                     "route_ids", "route_names"}, context)
        require(type(row["stop_id"]) is str and row["stop_id"]
                and row["stop_id"] not in seen_stops, f"{context}: repeated/blank stop_id")
        seen_stops.add(row["stop_id"])
        require(type(row["name"]) is str and row["name"]
                and type(row["stop_code"]) is str, f"{context}: invalid name/code")
        for key in ("codes", "child_stop_ids", "route_ids", "route_names"):
            require(type(row[key]) is list and all(type(item) is str and item for item in row[key])
                    and len(row[key]) == len(set(row[key])), f"{context}.{key}: invalid list")
        require(all(valid_code(code) for code in row["codes"]), f"{context}.codes: invalid code")
        require(row["codes"] == sorted(row["codes"]),
                f"{context}.codes: expected canonical order")
        require(row["child_stop_ids"] and row["route_ids"],
                f"{context}: active station needs child stops and routes")
        candidates = [station for station in stage1["stations"]
                      if set(row["codes"]) & set(station["codes"])]
        method = "station_code"
        if not candidates:
            without_codes = CODE_PATTERN.sub(" ", row["name"])
            gtfs_key = station_key(re.sub(r"[-–—()]+", " ", without_codes).strip())
            candidates = [station for station in stage1["stations"]
                          if station_key(station["name"]) == gtfs_key]
            method = "station_name"
        require(len(candidates) == 1,
                f"{context}: expected one Stage 1 match, found {len(candidates)} for "
                f"name={row['name']!r}, codes={row['codes']}")
        matches[candidates[0]["id"]].append({"match_method": method, **row})
    require(matches, "GTFS active inventory did not match any Stage 1 station")
    return {station_id: rows for station_id, rows in matches.items()}, [
        {"station_id": station_id, "name": station["name"], "codes": station["codes"]}
        for station_id, station in by_id.items() if station_id not in matches
    ]


MAX_ENVELOPE_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpGet(Protocol):
    def __call__(
        self, url: str, *, headers: Mapping[str, str], timeout: float, max_bytes: int
    ) -> HttpResponse: ...


@dataclass(frozen=True, slots=True)
class GtfsCollectionResult:
    output_directory: Path
    zip_path: Path
    snapshot_path: Path
    manifest_path: Path
    inventory: dict[str, object]


def _read_bounded(stream: object, *, url: str, max_bytes: int) -> bytes:
    body = stream.read(max_bytes + 1)  # type: ignore[attr-defined]
    if len(body) > max_bytes:
        raise GtfsSourceError(f"{url}: response exceeds {max_bytes} bytes")
    return body


def urllib_http_get(
    url: str,
    *,
    headers: Mapping[str, str],
    timeout: float,
    max_bytes: int,
) -> HttpResponse:
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return HttpResponse(
                int(response.status),
                {str(key): str(value) for key, value in response.headers.items()},
                _read_bounded(response, url=url, max_bytes=max_bytes),
            )
    except HTTPError as error:
        return HttpResponse(
            int(error.code),
            {str(key): str(value) for key, value in error.headers.items()},
            _read_bounded(error, url=url, max_bytes=max_bytes),
        )
    except (URLError, TimeoutError, OSError) as error:
        raise GtfsSourceError(
            f"{urlsplit(url).hostname}: transport failed: {type(error).__name__}: {error}"
        ) from error


def _strict_json(raw: bytes, context: str) -> object:
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, f"{context}: duplicate JSON key {key!r}")
            result[key] = value
        return result

    def constant(value):
        raise GtfsSourceError(f"{context}: non-finite number {value}")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GtfsSourceError(f"{context}: invalid UTF-8 JSON: {error}") from error


def _download_link(raw: bytes) -> tuple[str, dict[str, object]]:
    envelope = _strict_json(raw, "LTA GTFS response")
    require(type(envelope) is dict, "LTA GTFS response: expected object")
    require(
        set(envelope) in ({"value"}, {"odata.metadata", "value"}),
        f"LTA GTFS response: unexpected fields {sorted(envelope)}",
    )
    require(
        type(envelope["value"]) is list and len(envelope["value"]) == 1,
        "LTA GTFS response: expected exactly one download row",
    )
    row = envelope["value"][0]
    require(type(row) is dict, "LTA GTFS download row: expected object")
    if set(row) == {"Link"}:
        source_schema = "legacy_uppercase_link"
        link = row["Link"]
        source_timestamp = None
    elif set(row) == {"link", "timestamp"}:
        source_schema = "lowercase_link_with_timestamp"
        link = row["link"]
        source_timestamp = _source_timestamp(row["timestamp"])
    else:
        raise GtfsSourceError(
            "LTA GTFS download row: unsupported fields; expected exactly "
            "['Link'] or ['link', 'timestamp'], got " + repr(sorted(row))
        )
    require(type(link) is str and link and link == link.strip(), "LTA GTFS Link is invalid")
    parts = urlsplit(link)
    host = parts.hostname or ""
    require(
        parts.scheme == "https"
        and (host == "datamall.lta.gov.sg" or host.endswith(".amazonaws.com"))
        and parts.username is None
        and parts.password is None
        and parts.port in (None, 443)
        and not parts.fragment,
        "LTA GTFS returned an unexpected download URL",
    )
    redacted = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return link, {
        "schema_version": 1,
        "source_schema": source_schema,
        "value": [{
            "Link": redacted,
            "signed_url_sha256": digest(link.encode("utf-8")),
            **({"timestamp": source_timestamp} if source_timestamp is not None else {}),
        }],
    }


def _source_timestamp(value: object) -> str | int:
    """Validate the timestamp added to LTA's lowercase-link response schema."""

    if type(value) is int:
        require(0 <= value <= 9_999_999_999_999, "LTA GTFS timestamp integer is out of range")
        return value
    require(type(value) is str, "LTA GTFS timestamp must be an ISO-8601 string or Unix integer")
    require(value == value.strip() and 1 <= len(value) <= 64,
            "LTA GTFS timestamp string is blank, padded, or oversized")
    if value.isascii() and value.isdigit():
        require(len(value) in {10, 13}, "LTA GTFS numeric timestamp must use seconds or milliseconds")
        return value
    require(bool(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T.+", value)),
            "LTA GTFS timestamp must use ISO-8601 date-time syntax")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as error:
        raise GtfsSourceError("LTA GTFS timestamp is not a valid ISO-8601 date-time") from error
    return value


def _header(headers: Mapping[str, str], name: str) -> str | None:
    values = [value for key, value in headers.items() if key.casefold() == name.casefold()]
    require(len(values) <= 1, f"Repeated {name} response header")
    return values[0] if values else None


def _fetch_with_retry(
    url: str,
    *,
    headers: Mapping[str, str],
    timeout_seconds: float,
    max_bytes: int,
    max_attempts: int,
    initial_backoff_seconds: float,
    maximum_backoff_seconds: float,
    http_get: HttpGet,
    sleep: Callable[[float], None],
    failures: Path,
    prefix: str,
) -> HttpResponse:
    retryable_statuses = {408, 425, 429, 500, 502, 503, 504}
    response: HttpResponse | None = None
    last_error: GtfsSourceError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = http_get(
                url, headers=headers, timeout=timeout_seconds, max_bytes=max_bytes
            )
            last_error = None
        except GtfsSourceError as error:
            response = None
            last_error = error
        if response is not None and response.status == 200:
            return response
        if response is not None:
            name = f"{prefix}-attempt-{attempt:02d}-status-{response.status}.body"
            atomic_write_bytes(failures / name, response.body)
            retryable = response.status in retryable_statuses
        else:
            retryable = True
        if not retryable or attempt == max_attempts:
            if response is not None:
                raise GtfsSourceError(
                    f"{prefix}: HTTP {response.status} after {attempt} attempt(s); "
                    f"response retained in failures/{name}"
                )
            assert last_error is not None
            raise GtfsSourceError(
                f"{prefix}: transport failed after {attempt} attempt(s): {last_error}"
            ) from last_error
        delay = min(maximum_backoff_seconds, initial_backoff_seconds * 2 ** (attempt - 1))
        retry_after = _header(response.headers, "Retry-After") if response else None
        if retry_after and retry_after.isdigit():
            delay = min(maximum_backoff_seconds, max(delay, float(retry_after)))
        sleep(delay)
    raise AssertionError("unreachable retry loop")


def collect_lta_gtfs(
    *,
    endpoint: str,
    account_key: str,
    service_date: date,
    output_directory: str | Path,
    timeout_seconds: float,
    max_attempts: int,
    initial_backoff_seconds: float,
    maximum_backoff_seconds: float,
    http_get: HttpGet = urllib_http_get,
    sleep: Callable[[float], None] = time.sleep,
) -> GtfsCollectionResult:
    """Download the signed train GTFS ZIP and derive the active inventory."""

    require(type(account_key) is str and bool(account_key), "LTA AccountKey must be nonempty")
    require(type(service_date) is date, "service_date must be a date")
    require(type(timeout_seconds) is float and timeout_seconds > 0, "timeout_seconds must be positive")
    require(type(max_attempts) is int and not isinstance(max_attempts, bool) and max_attempts >= 1,
            "max_attempts must be a positive integer")
    for name, value in (
        ("initial_backoff_seconds", initial_backoff_seconds),
        ("maximum_backoff_seconds", maximum_backoff_seconds),
    ):
        require(type(value) is float and math.isfinite(value) and value >= 0,
                f"{name} must be a non-negative finite float")
    require(maximum_backoff_seconds >= initial_backoff_seconds,
            "maximum_backoff_seconds must be at least initial_backoff_seconds")
    parts = urlsplit(endpoint)
    require(
        parts.scheme == "https" and parts.hostname == "datamall2.mytransport.sg"
        and not parts.query and not parts.fragment and parts.username is None
        and parts.password is None and parts.port in (None, 443),
        "LTA GTFS endpoint is unexpected",
    )

    root = Path(output_directory)
    try:
        root.mkdir(parents=True)
    except FileExistsError as error:
        raise OutputExistsError(f"Refusing to reuse existing GTFS directory: {root}") from error
    failures = root / "failures"
    failures.mkdir()
    response = _fetch_with_retry(
        endpoint,
        headers={
            "Accept": "application/json",
            "AccountKey": account_key,
            "User-Agent": f"mrt-wayfinding/{__version__}",
        },
        timeout_seconds=timeout_seconds,
        max_bytes=MAX_ENVELOPE_BYTES,
        max_attempts=max_attempts,
        initial_backoff_seconds=initial_backoff_seconds,
        maximum_backoff_seconds=maximum_backoff_seconds,
        http_get=http_get,
        sleep=sleep,
        failures=failures,
        prefix="endpoint",
    )
    content_type = _header(response.headers, "Content-Type")
    if (
        content_type is None
        or content_type.split(";", 1)[0].strip().casefold() != "application/json"
    ):
        atomic_write_bytes(failures / "endpoint-unexpected-content-type.body", response.body)
        raise GtfsSourceError(
            f"LTA GTFS endpoint: expected application/json, got {content_type!r}; "
            "response retained in failures/endpoint-unexpected-content-type.body"
        )
    link, redacted = _download_link(response.body)
    # The raw envelope contains a temporary signed download URL. Retain its
    # hash and a query-redacted form instead of persisting bearer material.
    atomic_write_json(root / "source-envelope.redacted.json", redacted)
    download = _fetch_with_retry(
        link,
        headers={"Accept": "application/zip", "User-Agent": f"mrt-wayfinding/{__version__}"},
        timeout_seconds=timeout_seconds,
        max_bytes=MAX_ZIP_BYTES,
        max_attempts=max_attempts,
        initial_backoff_seconds=initial_backoff_seconds,
        maximum_backoff_seconds=maximum_backoff_seconds,
        http_get=http_get,
        sleep=sleep,
        failures=failures,
        prefix="download",
    )
    zip_path = atomic_write_bytes(root / "GTFSScheduleTrain.zip", download.body)
    inventory = active_inventory(download.body, service_date)
    snapshot_path = atomic_write_json(root / "active-stations.json", inventory)
    manifest_path = atomic_write_json(root / "collection.json", {
        "schema_version": 1,
        "kind": "lta_gtfs_train_collection",
        "endpoint": endpoint,
        "source_schema": redacted["source_schema"],
        "source_timestamp": redacted["value"][0].get("timestamp"),
        "service_date": service_date.isoformat(),
        "download_url": redacted["value"][0]["Link"],
        "signed_url_sha256": redacted["value"][0]["signed_url_sha256"],
        "zip_sha256": inventory["source_zip_sha256"],
        "feed_info": inventory["feed_info"],
        "active_stations_sha256": digest(snapshot_path.read_bytes()),
        "active_station_count": inventory["active_station_count"],
    })
    return GtfsCollectionResult(root, zip_path, snapshot_path, manifest_path, inventory)


def match_catalog_stations(
    catalog: StationCatalog, inventory: object
) -> tuple[dict[str, list[dict[str, object]]], list[dict[str, object]]]:
    """Match every active GTFS station to one registry station; return inactive registry rows."""

    fields(inventory, {
        "schema_version", "status", "kind", "service_date", "source_zip_sha256", "feed_info",
        "active_service_count", "active_trip_count", "active_station_count", "stations",
    }, "GTFS active inventory")
    require(
        inventory["schema_version"] == 1
        and inventory["status"] == "complete"
        and inventory["kind"] == "lta_gtfs_active_train_stations",
        "Unsupported or incomplete GTFS active inventory",
    )
    require(
        type(inventory["service_date"]) is str
        and bool(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", inventory["service_date"])),
        "GTFS active inventory.service_date: expected YYYY-MM-DD",
    )
    service_date = _gtfs_date(
        inventory["service_date"].replace("-", ""), "GTFS active inventory.service_date"
    )
    _validate_normalized_feed_information(
        inventory["feed_info"], service_date, "GTFS active inventory.feed_info"
    )
    require(
        type(inventory["source_zip_sha256"]) is str
        and bool(re.fullmatch(r"[0-9a-f]{64}", inventory["source_zip_sha256"])),
        "GTFS active inventory.source_zip_sha256: invalid hash",
    )
    for key in ("active_service_count", "active_trip_count", "active_station_count"):
        require(
            type(inventory[key]) is int and not isinstance(inventory[key], bool) and inventory[key] > 0,
            f"GTFS active inventory.{key}: expected positive integer",
        )
    require(type(inventory["stations"]) is list, "GTFS active inventory.stations must be an array")
    require(
        type(inventory["active_station_count"]) is int
        and inventory["active_station_count"] == len(inventory["stations"]),
        "GTFS active station count disagrees with rows",
    )
    matches: dict[str, list[dict[str, object]]] = defaultdict(list)
    seen_stops: set[str] = set()
    for index, row in enumerate(inventory["stations"]):
        context = f"GTFS active stations[{index}]"
        fields(row, {"stop_id", "stop_code", "name", "codes", "child_stop_ids",
                     "route_ids", "route_names"}, context)
        require(type(row["stop_id"]) is str and row["stop_id"] and row["stop_id"] not in seen_stops,
                f"{context}: repeated or blank stop_id")
        seen_stops.add(row["stop_id"])
        require(
            type(row["name"]) is str and row["name"] and type(row["stop_code"]) is str,
            f"{context}: invalid name or stop_code",
        )
        for key in ("codes", "child_stop_ids", "route_ids", "route_names"):
            require(
                type(row[key]) is list
                and all(type(item) is str and item for item in row[key])
                and len(row[key]) == len(set(row[key])),
                f"{context}.{key}: invalid string array",
            )
        require(all(valid_code(code) for code in row["codes"]), f"{context}.codes: invalid")
        require(
            row["codes"] == sorted(row["codes"]),
            f"{context}.codes: expected canonical order",
        )
        require(
            bool(row["child_stop_ids"]) and bool(row["route_ids"]),
            f"{context}: active station needs child stops and routes",
        )
        candidates = [station for station in catalog.stations
                      if set(row["codes"]) & set(station.codes)]
        method = "station_code"
        if not candidates:
            without_codes = CODE_PATTERN.sub(" ", row["name"])
            key = station_key(re.sub(r"[-–—()]+", " ", without_codes).strip())
            candidates = [station for station in catalog.stations if station_key(station.name) == key]
            method = "station_name"
        require(
            len(candidates) == 1,
            f"{context}: expected one registry match, found {len(candidates)} for "
            f"name={row['name']!r}, codes={row['codes']}",
        )
        matches[candidates[0].name].append({"match_method": method, **row})
    require(matches, "GTFS active inventory did not match the station registry")
    inactive = [
        {"name": station.name, "codes": list(station.codes), "catalog_status": station.catalog_status}
        for station in catalog.stations if station.name not in matches
    ]
    return dict(matches), inactive


__all__ = [
    "GtfsCollectionResult",
    "GtfsSourceError",
    "HttpResponse",
    "active_inventory",
    "collect_lta_gtfs",
    "match_catalog_stations",
]
