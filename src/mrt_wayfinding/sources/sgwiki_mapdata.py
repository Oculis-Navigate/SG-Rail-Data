"""Strict collection and normalization of SgWiki Kartographer overlays.

Kartographer pages render an OpenStreetMap basemap with a revision-specific
overlay authored on SgWiki.  This module treats the overlay as SgWiki evidence:
it does not claim that the exit markers came from live OpenStreetMap data.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .. import __version__
from ..progress import ProgressCallback
from ..station_catalog import station_key, station_slug
from ..storage import OutputExistsError, atomic_write_bytes, atomic_write_json
from .sgwiki import HttpGet, HttpResponse, urllib_http_get
from .sgwiki_parser import SgWikiSourceError


MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_OVERLAY_ID = re.compile(r"_[0-9a-f]{40}\Z")
_EXIT_LABEL = re.compile(r"(?:[A-Z][0-9]?|[1-9][0-9]?)\Z")
_COLOUR = re.compile(r"#[0-9A-Fa-f]{6}\Z")
_MAP_ATTRIBUTES = frozenset({
    "class",
    "data-height",
    "data-lat",
    "data-lon",
    "data-mw-kartographer",
    "data-overlays",
    "data-style",
    "data-width",
    "data-zoom",
    "href",
    "style",
})


class SgWikiMapDataError(SgWikiSourceError):
    """Raised when Kartographer markup, transport, or GeoJSON changes."""


@dataclass(frozen=True, slots=True)
class SgWikiMapDataCollectionResult:
    output_directory: Path
    snapshot_path: Path
    manifest_path: Path
    stations: tuple[dict[str, object], ...]
    no_map_station_names: tuple[str, ...]


def _duplicate_key(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SgWikiMapDataError(f"JSON object contains duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise SgWikiMapDataError(f"JSON contains non-finite number {value}")


def _loads_strict(raw: bytes | str, *, context: str) -> object:
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SgWikiMapDataError(f"{context}: response is not UTF-8") from error
    elif isinstance(raw, str):
        text = raw
    else:
        raise SgWikiMapDataError(f"{context}: expected JSON bytes or text")
    try:
        return json.loads(
            text,
            object_pairs_hook=_duplicate_key,
            parse_constant=_reject_constant,
        )
    except SgWikiMapDataError as error:
        raise SgWikiMapDataError(f"{context}: {error}") from error
    except json.JSONDecodeError as error:
        raise SgWikiMapDataError(f"{context}: invalid JSON: {error}") from error


def _mapping(value: object, *, context: str) -> dict[str, object]:
    if type(value) is not dict:
        raise SgWikiMapDataError(f"{context}: expected object")
    return value


def _list(value: object, *, context: str) -> list[object]:
    if type(value) is not list:
        raise SgWikiMapDataError(f"{context}: expected array")
    return value


def _text(value: object, *, context: str) -> str:
    if type(value) is not str or not value:
        raise SgWikiMapDataError(f"{context}: expected nonempty text")
    return value


def _integer(value: object, *, context: str, minimum: int = 1) -> int:
    if type(value) is not int or type(value) is bool or value < minimum:
        raise SgWikiMapDataError(f"{context}: expected integer >= {minimum}")
    return value


def _exact_fields(value: Mapping[str, object], expected: set[str], *, context: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        raise SgWikiMapDataError(
            f"{context}: unexpected fields; missing={missing}, extra={extra}"
        )


def _one_query(query: Mapping[str, list[str]], name: str) -> str:
    values = query.get(name)
    if values is None or len(values) != 1 or not values[0]:
        raise SgWikiMapDataError(f"Embedded SgWiki map has invalid {name}")
    return values[0]


def _mapdata_url(endpoint: str, revision_id: int, overlay_id: str) -> str:
    parts = urlsplit(endpoint)
    if (
        parts.scheme != "https"
        or parts.hostname != "sgwiki.miraheze.org"
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
    ):
        raise SgWikiMapDataError(
            "SgWiki MapData endpoint must be an unqualified https://sgwiki.miraheze.org URL"
        )
    _integer(revision_id, context="SgWiki map revision")
    if type(overlay_id) is not str or _OVERLAY_ID.fullmatch(overlay_id) is None:
        raise SgWikiMapDataError("Invalid SgWiki map overlay identifier")
    query = urlencode({
        "action": "query",
        "prop": "mapdata",
        "revids": revision_id,
        "mpdgroups": overlay_id,
        "mpdparser": "legacy",
        "format": "json",
        "formatversion": 2,
    })
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def discover_embedded_map(
    html: str,
    *,
    title: str,
    revision_id: int,
    endpoint: str,
) -> dict[str, object]:
    """Return revision-pinned map metadata or an explicit no-map result."""

    if type(html) is not str or not html:
        raise SgWikiMapDataError("SgWiki rendered HTML must be nonempty text")
    if type(title) is not str or not title.endswith(" station"):
        raise SgWikiMapDataError("Invalid SgWiki station title")
    _integer(revision_id, context="SgWiki map revision")
    soup = BeautifulSoup(html, "html.parser")
    candidates = soup.select("[data-mw-kartographer], .mw-kartographer-map")
    if not candidates:
        return {"rendered": False}
    maps = soup.select('a.mw-kartographer-map[data-mw-kartographer="mapframe"]')
    if len(maps) != 1 or len({id(node) for node in candidates}) != 1:
        raise SgWikiMapDataError("Unknown or repeated SgWiki Kartographer map markup")
    node = maps[0]
    if set(node.attrs) != _MAP_ATTRIBUTES:
        raise SgWikiMapDataError("SgWiki embedded-map attributes changed")
    if set(node.get("class", [])) != {"mw-kartographer-map", "notheme"}:
        raise SgWikiMapDataError("SgWiki embedded-map classes changed")
    height_text = node["data-height"]
    if (
        node["data-style"] != "."
        or node["data-width"] != "full"
        or node["style"] != f"width: 100%; height: {height_text}px;"
    ):
        raise SgWikiMapDataError("SgWiki embedded-map display contract changed")
    heading = node.find_previous(["h2", "h3"])
    if heading is None or heading.get_text(" ", strip=True) != "Entrances/Exits":
        raise SgWikiMapDataError("SgWiki map moved outside the Entrances/Exits section")

    zoom_text = node["data-zoom"]
    if not zoom_text.isdigit() or not 1 <= int(zoom_text) <= 22:
        raise SgWikiMapDataError("Invalid SgWiki map zoom")
    if not height_text.isdigit() or not 100 <= int(height_text) <= 2000:
        raise SgWikiMapDataError("Invalid SgWiki map height")
    try:
        latitude = float(node["data-lat"])
        longitude = float(node["data-lon"])
    except ValueError as error:
        raise SgWikiMapDataError("Invalid SgWiki map centre") from error
    _validate_coordinate([longitude, latitude], context="SgWiki map centre")

    overlays = _loads_strict(node["data-overlays"], context="SgWiki map overlays")
    if (
        type(overlays) is not list
        or len(overlays) != 1
        or type(overlays[0]) is not str
        or _OVERLAY_ID.fullmatch(overlays[0]) is None
    ):
        raise SgWikiMapDataError("Unknown SgWiki map overlay identifier")
    overlay_id = overlays[0]

    images = node.find_all("img", recursive=False)
    if len(images) != 1 or set(images[0].attrs) != {
        "src", "width", "height", "decoding", "alt"
    }:
        raise SgWikiMapDataError("SgWiki map snapshot image markup changed")
    image = images[0]
    if (
        image["width"] != "1024"
        or image["height"] != height_text
        or image["decoding"] != "async"
        or image["alt"] != "Map"
    ):
        raise SgWikiMapDataError("SgWiki map snapshot dimensions or metadata changed")
    image_url = urlsplit(image["src"])
    if (
        image_url.scheme != "https"
        or image_url.netloc != "tile.openstreetmap.org"
        or not image_url.path.startswith("/img/")
        or not image_url.path.endswith(".png")
    ):
        raise SgWikiMapDataError("SgWiki map snapshot is no longer an OpenStreetMap render")
    try:
        query = parse_qs(image_url.query, strict_parsing=True)
    except ValueError as error:
        raise SgWikiMapDataError("SgWiki map snapshot query changed") from error
    if set(query) != {"domain", "groups", "lang", "parser", "revid", "title"}:
        raise SgWikiMapDataError("SgWiki map snapshot query changed")
    if (
        _one_query(query, "domain") != "sgwiki.miraheze.org"
        or _one_query(query, "groups") != overlay_id
        or _one_query(query, "lang") != "en"
        or _one_query(query, "parser") != "legacy"
        or _one_query(query, "revid") != str(revision_id)
        or _one_query(query, "title") != title.replace(" ", "_")
    ):
        raise SgWikiMapDataError(
            "SgWiki map snapshot provenance disagrees with the station page"
        )
    expected_href = (
        f"/wiki/Special:Map/{zoom_text}/{node['data-lat']}/{node['data-lon']}/en"
    )
    if node["href"] != expected_href:
        raise SgWikiMapDataError("SgWiki map link disagrees with its centre")
    return {
        "rendered": True,
        "kind": "kartographer_osm_basemap_with_sgwiki_overlay",
        "zoom": int(zoom_text),
        "center": [longitude, latitude],
        "width": 1024,
        "height": int(height_text),
        "overlay_id": overlay_id,
        "map_href": node["href"],
        "snapshot_url": image["src"],
        "mapdata_url": _mapdata_url(endpoint, revision_id, overlay_id),
    }


def _number(value: object, *, context: str) -> int | float:
    if type(value) not in (int, float) or type(value) is bool or not math.isfinite(value):
        raise SgWikiMapDataError(f"{context}: expected finite number")
    return value


def _validate_coordinate(value: object, *, context: str) -> list[int | float]:
    coordinates = _list(value, context=context)
    if len(coordinates) != 2:
        raise SgWikiMapDataError(f"{context}: expected [longitude, latitude]")
    longitude = _number(coordinates[0], context=f"{context}[0]")
    latitude = _number(coordinates[1], context=f"{context}[1]")
    if not 103 <= longitude <= 105 or not 1 <= latitude <= 2:
        raise SgWikiMapDataError(f"{context}: coordinate is outside Singapore")
    return [longitude, latitude]


def _colour(value: object, *, context: str) -> str:
    colour = _text(value, context=context)
    if _COLOUR.fullmatch(colour) is None:
        raise SgWikiMapDataError(f"{context}: expected #RRGGBB colour")
    return colour


def _normalize_feature(
    value: object,
    *,
    feature_index: int,
) -> tuple[dict[str, object], dict[str, object] | None]:
    context = f"SgWiki MapData feature[{feature_index}]"
    feature = _mapping(value, context=context)
    _exact_fields(feature, {"type", "geometry", "properties"}, context=context)
    if feature["type"] != "Feature":
        raise SgWikiMapDataError(f"{context}.type: expected 'Feature'")
    geometry = _mapping(feature["geometry"], context=f"{context}.geometry")
    _exact_fields(geometry, {"type", "coordinates"}, context=f"{context}.geometry")
    geometry_type = geometry["type"]
    properties = _mapping(feature["properties"], context=f"{context}.properties")

    exit_marker: dict[str, object] | None = None
    if geometry_type == "LineString":
        coordinates = _list(geometry["coordinates"], context=f"{context}.geometry.coordinates")
        if len(coordinates) < 2:
            raise SgWikiMapDataError(f"{context}: LineString needs at least two positions")
        normalized_coordinates = [
            _validate_coordinate(item, context=f"{context}.geometry.coordinates[{index}]")
            for index, item in enumerate(coordinates)
        ]
        allowed = (
            {"stroke", "stroke-width"},
            {"fill-opacity", "stroke", "stroke-width"},
        )
        if set(properties) not in allowed:
            expected = "{stroke, stroke-width} or {fill-opacity, stroke, stroke-width}"
            raise SgWikiMapDataError(
                f"{context}.properties: unexpected line style fields; expected {expected}"
            )
        _colour(properties["stroke"], context=f"{context}.properties.stroke")
        _number(properties["stroke-width"], context=f"{context}.properties.stroke-width")
        if "fill-opacity" in properties:
            opacity = _number(
                properties["fill-opacity"], context=f"{context}.properties.fill-opacity"
            )
            if not 0 <= opacity <= 1:
                raise SgWikiMapDataError(f"{context}.properties.fill-opacity: outside 0..1")
    elif geometry_type == "Point":
        normalized_coordinates = _validate_coordinate(
            geometry["coordinates"], context=f"{context}.geometry.coordinates"
        )
        symbol = _text(
            properties.get("marker-symbol"), context=f"{context}.properties.marker-symbol"
        )
        if symbol == "rail":
            expected_fields = {"marker-color", "marker-symbol", "title"}
        elif symbol == "bus" or symbol.startswith("exit-"):
            expected_fields = {"description", "marker-color", "marker-symbol", "title"}
        else:
            raise SgWikiMapDataError(
                f"{context}.properties.marker-symbol: unsupported value {symbol!r}"
            )
        _exact_fields(properties, expected_fields, context=f"{context}.properties")
        _colour(properties["marker-color"], context=f"{context}.properties.marker-color")
        title = _text(properties["title"], context=f"{context}.properties.title")
        if "description" in properties:
            description = _text(
                properties["description"], context=f"{context}.properties.description"
            )
        if symbol.startswith("exit-"):
            label = symbol.removeprefix("exit-")
            if _EXIT_LABEL.fullmatch(label) is None or title != f"Exit {label}":
                raise SgWikiMapDataError(
                    f"{context}: ambiguous exit label between marker symbol {symbol!r} "
                    f"and title {title!r}"
                )
            exit_marker = {
                "label": label,
                "title": title,
                "destination_verbatim": description,
                "coordinates": normalized_coordinates,
                "feature_index": feature_index,
            }
    else:
        raise SgWikiMapDataError(f"{context}.geometry.type: unsupported value {geometry_type!r}")

    normalized = {
        "type": "Feature",
        "geometry": {"type": geometry_type, "coordinates": normalized_coordinates},
        "properties": dict(properties),
    }
    return normalized, exit_marker


def parse_mapdata_response(
    raw: bytes,
    *,
    station_name: str,
    codes: list[str] | tuple[str, ...],
    title: str,
    page_id: int,
    revision_id: int,
    overlay_id: str,
    map_metadata: Mapping[str, object],
) -> dict[str, object]:
    """Validate the complete response and produce one normalized overlay."""

    station_name = _text(station_name, context="SgWiki MapData station name")
    if type(codes) not in (list, tuple) or not codes or any(
        type(code) is not str or not code for code in codes
    ):
        raise SgWikiMapDataError("SgWiki MapData station codes must be nonempty text")
    title = _text(title, context="SgWiki MapData title")
    page_id = _integer(page_id, context="SgWiki MapData page ID")
    revision_id = _integer(revision_id, context="SgWiki MapData revision ID")
    if type(overlay_id) is not str or _OVERLAY_ID.fullmatch(overlay_id) is None:
        raise SgWikiMapDataError("Invalid SgWiki map overlay identifier")

    document = _mapping(_loads_strict(raw, context="SgWiki MapData"), context="SgWiki MapData")
    _exact_fields(document, {"batchcomplete", "query"}, context="SgWiki MapData envelope")
    if document["batchcomplete"] not in (True, ""):
        raise SgWikiMapDataError("SgWiki MapData batch is incomplete")
    query = _mapping(document["query"], context="SgWiki MapData query")
    _exact_fields(query, {"pages"}, context="SgWiki MapData query")
    pages = _list(query["pages"], context="SgWiki MapData pages")
    if len(pages) != 1:
        raise SgWikiMapDataError("SgWiki MapData expected exactly one page")
    page = _mapping(pages[0], context="SgWiki MapData page")
    _exact_fields(
        page,
        {"pageid", "ns", "title", "revid", "mapdata"},
        context="SgWiki MapData page",
    )
    if (
        page["pageid"] != page_id
        or page["ns"] != 0
        or page["title"] != title
        or page["revid"] != revision_id
    ):
        raise SgWikiMapDataError(
            "SgWiki MapData page identity disagrees with saved station evidence"
        )
    payloads = _list(page["mapdata"], context="SgWiki MapData page.mapdata")
    if len(payloads) != 1 or type(payloads[0]) is not str or not payloads[0]:
        raise SgWikiMapDataError("SgWiki MapData page has an unknown payload wrapper")
    groups = _mapping(
        _loads_strict(payloads[0], context="SgWiki MapData embedded payload"),
        context="SgWiki MapData embedded payload",
    )
    if set(groups) != {overlay_id}:
        raise SgWikiMapDataError("SgWiki MapData returned an unexpected overlay set")
    overlay = _list(groups[overlay_id], context="SgWiki MapData overlay")
    if len(overlay) != 1:
        raise SgWikiMapDataError("SgWiki MapData overlay expected one FeatureCollection")
    collection = _mapping(overlay[0], context="SgWiki MapData FeatureCollection")
    _exact_fields(
        collection, {"type", "features"}, context="SgWiki MapData FeatureCollection"
    )
    if collection["type"] != "FeatureCollection":
        raise SgWikiMapDataError("SgWiki MapData overlay is not a FeatureCollection")
    features = _list(collection["features"], context="SgWiki MapData features")
    normalized_features: list[dict[str, object]] = []
    exit_markers: list[dict[str, object]] = []
    labels: set[str] = set()
    for feature_index, value in enumerate(features):
        normalized, exit_marker = _normalize_feature(value, feature_index=feature_index)
        normalized_features.append(normalized)
        if exit_marker is not None:
            label = exit_marker["label"]
            assert type(label) is str
            if label in labels:
                raise SgWikiMapDataError(
                    f"SgWiki MapData contains duplicate exit label {label!r}"
                )
            labels.add(label)
            exit_markers.append(exit_marker)

    return {
        "schema_version": 1,
        "kind": "sgwiki_kartographer_overlay",
        "station": station_name,
        "codes": list(codes),
        "page": {
            "title": title,
            "page_id": page_id,
            "revision_id": revision_id,
            "overlay_id": overlay_id,
        },
        "map": dict(map_metadata),
        "feature_collection": {
            "type": "FeatureCollection",
            "features": normalized_features,
        },
        "exit_markers": exit_markers,
    }


def _header(headers: Mapping[str, str], name: str) -> str | None:
    values = [value for key, value in headers.items() if key.casefold() == name.casefold()]
    if len(values) > 1:
        raise SgWikiMapDataError(f"SgWiki MapData response has repeated {name} headers")
    return values[0] if values else None


def _delay(
    response: HttpResponse | None,
    *,
    attempt: int,
    initial: float,
    maximum: float,
) -> float:
    exponential = min(maximum, initial * (2 ** (attempt - 1)))
    if response is None:
        return exponential
    retry_after = _header(response.headers, "Retry-After")
    if retry_after is None or not retry_after.isdigit():
        return exponential
    return min(maximum, max(exponential, float(retry_after)))


def _validate_collection_parameters(
    *,
    timeout_seconds: float,
    max_attempts: int,
    initial_backoff_seconds: float,
    maximum_backoff_seconds: float,
    minimum_interval_seconds: float,
) -> None:
    if type(timeout_seconds) is not float or timeout_seconds <= 0:
        raise SgWikiMapDataError("timeout_seconds must be a positive float")
    if type(max_attempts) is not int or type(max_attempts) is bool or max_attempts < 1:
        raise SgWikiMapDataError("max_attempts must be a positive integer")
    for name, value in (
        ("initial_backoff_seconds", initial_backoff_seconds),
        ("maximum_backoff_seconds", maximum_backoff_seconds),
        ("minimum_interval_seconds", minimum_interval_seconds),
    ):
        if type(value) is not float or not math.isfinite(value) or value < 0:
            raise SgWikiMapDataError(f"{name} must be a non-negative finite float")
    if maximum_backoff_seconds < initial_backoff_seconds:
        raise SgWikiMapDataError(
            "maximum_backoff_seconds must be at least initial_backoff_seconds"
        )


def _load_sgwiki_evidence(root: Path) -> list[dict[str, object]]:
    snapshot_raw = (root / "snapshot.json").read_bytes()
    collection_raw = (root / "collection.json").read_bytes()
    snapshot = _mapping(
        _loads_strict(snapshot_raw, context="SgWiki station snapshot"),
        context="SgWiki station snapshot",
    )
    _exact_fields(
        snapshot, {"schema_version", "kind", "stations"}, context="SgWiki station snapshot"
    )
    if snapshot["schema_version"] != 1 or snapshot["kind"] != "sgwiki_station_snapshot":
        raise SgWikiMapDataError("Unsupported SgWiki station snapshot schema")
    stations = _list(snapshot["stations"], context="SgWiki station snapshot.stations")
    collection = _mapping(
        _loads_strict(collection_raw, context="SgWiki station collection"),
        context="SgWiki station collection",
    )
    _exact_fields(
        collection,
        {
            "schema_version", "kind", "endpoint", "station_count",
            "complete_station_count", "partial_station_count", "partial_station_names",
            "snapshot_sha256", "pages",
        },
        context="SgWiki station collection",
    )
    if collection["schema_version"] != 1 or collection["kind"] != "sgwiki_station_collection":
        raise SgWikiMapDataError("Unsupported SgWiki station collection schema")
    if collection["snapshot_sha256"] != sha256(snapshot_raw).hexdigest():
        raise SgWikiMapDataError("SgWiki station snapshot hash disagrees with collection")
    pages = _list(collection["pages"], context="SgWiki station collection.pages")
    if collection["station_count"] != len(stations) or len(pages) != len(stations):
        raise SgWikiMapDataError("SgWiki station snapshot and collection counts disagree")

    page_by_name: dict[str, dict[str, object]] = {}
    for index, raw_page in enumerate(pages):
        page = _mapping(raw_page, context=f"SgWiki station collection.pages[{index}]")
        _exact_fields(
            page,
            {
                "station", "response_file", "response_sha256", "rendered_file",
                "rendered_sha256", "page_id", "revision_id", "status",
            },
            context=f"SgWiki station collection.pages[{index}]",
        )
        name = _text(page["station"], context=f"SgWiki station collection.pages[{index}].station")
        if name in page_by_name:
            raise SgWikiMapDataError(f"Repeated SgWiki station evidence for {name}")
        page_by_name[name] = page

    result: list[dict[str, object]] = []
    for index, raw_station in enumerate(stations):
        station = _mapping(raw_station, context=f"SgWiki station snapshot.stations[{index}]")
        _exact_fields(
            station,
            {"name", "codes", "catalog_status", "sgwiki_title", "source"},
            context=f"SgWiki station snapshot.stations[{index}]",
        )
        name = _text(station["name"], context=f"SgWiki station snapshot.stations[{index}].name")
        page = page_by_name.get(name)
        if page is None:
            raise SgWikiMapDataError(f"Missing SgWiki collection evidence for {name}")
        source = _mapping(station["source"], context=f"SgWiki station source for {name}")
        title = _text(source.get("title"), context=f"SgWiki station source title for {name}")
        page_id = _integer(source.get("page_id"), context=f"SgWiki page ID for {name}")
        revision_id = _integer(
            source.get("revision_id"), context=f"SgWiki revision ID for {name}"
        )
        if (
            station_key(title) != station_key(name)
            or page["page_id"] != page_id
            or page["revision_id"] != revision_id
        ):
            raise SgWikiMapDataError(f"SgWiki snapshot provenance disagrees for {name}")
        rendered_relative = Path(
            _text(page["rendered_file"], context=f"SgWiki rendered path for {name}")
        )
        rendered_path = (root / rendered_relative).resolve()
        try:
            rendered_path.relative_to(root.resolve())
        except ValueError as error:
            raise SgWikiMapDataError(f"SgWiki rendered path escapes source root for {name}") from error
        if not rendered_path.is_file():
            raise SgWikiMapDataError(f"Missing saved SgWiki rendered page for {name}")
        rendered_raw = rendered_path.read_bytes()
        if sha256(rendered_raw).hexdigest() != page["rendered_sha256"]:
            raise SgWikiMapDataError(f"Saved SgWiki rendered page hash changed for {name}")
        try:
            html = rendered_raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SgWikiMapDataError(f"Saved SgWiki rendered page is not UTF-8 for {name}") from error
        codes = station["codes"]
        if type(codes) is not list or not codes or any(type(code) is not str or not code for code in codes):
            raise SgWikiMapDataError(f"SgWiki station codes are invalid for {name}")
        result.append({
            "station": name,
            "codes": list(codes),
            "title": title,
            "page_id": page_id,
            "revision_id": revision_id,
            "html": html,
            "rendered_file": str(rendered_relative),
            "rendered_sha256": page["rendered_sha256"],
            "snapshot_sha256": collection["snapshot_sha256"],
        })
    if set(page_by_name) != {record["station"] for record in result}:
        raise SgWikiMapDataError("SgWiki collection contains an unknown station page")
    return result


def collect_sgwiki_mapdata(
    *,
    endpoint: str,
    sgwiki_directory: str | Path,
    output_directory: str | Path,
    timeout_seconds: float,
    max_attempts: int,
    initial_backoff_seconds: float,
    maximum_backoff_seconds: float,
    minimum_interval_seconds: float,
    http_get: HttpGet = urllib_http_get,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    progress: ProgressCallback | None = None,
) -> SgWikiMapDataCollectionResult:
    """Collect every embedded map discovered in one immutable SgWiki run."""

    _validate_collection_parameters(
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        initial_backoff_seconds=initial_backoff_seconds,
        maximum_backoff_seconds=maximum_backoff_seconds,
        minimum_interval_seconds=minimum_interval_seconds,
    )
    source_root = Path(sgwiki_directory).resolve()
    evidence = _load_sgwiki_evidence(source_root)
    root = Path(output_directory)
    try:
        root.mkdir(parents=True)
    except FileExistsError as error:
        raise OutputExistsError(f"Refusing to reuse existing MapData directory: {root}") from error
    raw_directory = root / "responses"
    overlay_directory = root / "overlays"
    failures = root / "failures"
    raw_directory.mkdir()
    overlay_directory.mkdir()
    failures.mkdir()

    headers = {
        "Accept": "application/json",
        "User-Agent": f"mrt-wayfinding/{__version__} (Singapore rail wayfinding dataset)",
    }
    rows: list[dict[str, object]] = []
    overlays: list[dict[str, object]] = []
    no_map_names: list[str] = []
    previous_started: float | None = None
    for station_index, record in enumerate(evidence):
        station_name = str(record["station"])
        if progress is not None:
            progress("SgWiki maps", station_index, len(evidence), station_name)
        map_metadata = discover_embedded_map(
            str(record["html"]),
            title=str(record["title"]),
            revision_id=int(record["revision_id"]),
            endpoint=endpoint,
        )
        base_provenance = {
            "station": station_name,
            "codes": record["codes"],
            "title": record["title"],
            "page_id": record["page_id"],
            "revision_id": record["revision_id"],
            "rendered_file": record["rendered_file"],
            "rendered_sha256": record["rendered_sha256"],
            "sgwiki_snapshot_sha256": record["snapshot_sha256"],
        }
        if not map_metadata["rendered"]:
            rows.append({**base_provenance, "status": "no_embedded_map"})
            no_map_names.append(station_name)
            if progress is not None:
                progress("SgWiki maps", station_index + 1, len(evidence), station_name)
            continue

        url = str(map_metadata["mapdata_url"])
        response: HttpResponse | None = None
        last_error: SgWikiSourceError | None = None
        for attempt in range(1, max_attempts + 1):
            if previous_started is not None:
                remaining = minimum_interval_seconds - (monotonic() - previous_started)
                if remaining > 0:
                    sleep(remaining)
            previous_started = monotonic()
            try:
                response = http_get(url, headers=headers, timeout=timeout_seconds)
                last_error = None
            except SgWikiSourceError as error:
                response = None
                last_error = error
            if response is not None and response.status == 200:
                break
            if response is not None:
                failure_name = (
                    f"{station_index:03d}-{station_slug(station_name)}-attempt-{attempt:02d}"
                    f"-status-{response.status}.body"
                )
                atomic_write_bytes(failures / failure_name, response.body)
                retryable = response.status in _RETRYABLE_STATUSES
            else:
                retryable = True
            if not retryable or attempt == max_attempts:
                if response is not None:
                    raise SgWikiMapDataError(
                        f"{station_name}: MapData HTTP {response.status} after {attempt} "
                        f"attempt(s); response retained in failures/{failure_name}"
                    )
                assert last_error is not None
                raise SgWikiMapDataError(
                    f"{station_name}: MapData transport failed after {attempt} attempt(s): "
                    f"{last_error}"
                ) from last_error
            sleep(_delay(
                response,
                attempt=attempt,
                initial=initial_backoff_seconds,
                maximum=maximum_backoff_seconds,
            ))
        assert response is not None and response.status == 200
        content_type = _header(response.headers, "Content-Type")
        if content_type is None or content_type.split(";", 1)[0].strip().casefold() != "application/json":
            failure_name = f"{station_index:03d}-{station_slug(station_name)}-content-type.body"
            atomic_write_bytes(failures / failure_name, response.body)
            raise SgWikiMapDataError(
                f"{station_name}: expected MapData application/json, got {content_type!r}; "
                f"response retained in failures/{failure_name}"
            )
        response_name = f"{station_index:03d}-{station_slug(station_name)}.json"
        response_path = atomic_write_bytes(raw_directory / response_name, response.body)
        normalized = parse_mapdata_response(
            response.body,
            station_name=station_name,
            codes=list(record["codes"]),  # type: ignore[arg-type]
            title=str(record["title"]),
            page_id=int(record["page_id"]),
            revision_id=int(record["revision_id"]),
            overlay_id=str(map_metadata["overlay_id"]),
            map_metadata=map_metadata,
        )
        overlay_name = f"{station_index:03d}-{station_slug(station_name)}.json"
        overlay_path = atomic_write_json(overlay_directory / overlay_name, normalized)
        row = {
            **base_provenance,
            "status": "downloaded",
            "overlay_id": map_metadata["overlay_id"],
            "mapdata_url": url,
            "response_file": f"responses/{response_name}",
            "response_sha256": sha256(response_path.read_bytes()).hexdigest(),
            "normalized_file": f"overlays/{overlay_name}",
            "normalized_sha256": sha256(overlay_path.read_bytes()).hexdigest(),
            "exit_marker_count": len(normalized["exit_markers"]),
        }
        rows.append(row)
        overlays.append(normalized)
        if progress is not None:
            progress("SgWiki maps", station_index + 1, len(evidence), station_name)

    snapshot_path = atomic_write_json(root / "snapshot.json", {
        "schema_version": 1,
        "kind": "sgwiki_kartographer_snapshot",
        "stations": rows,
        "overlays": overlays,
    })
    manifest_path = atomic_write_json(root / "collection.json", {
        "schema_version": 1,
        "kind": "sgwiki_kartographer_collection",
        "endpoint": endpoint,
        "station_count": len(rows),
        "embedded_map_count": len(overlays),
        "no_embedded_map_count": len(no_map_names),
        "no_embedded_map_station_names": no_map_names,
        "snapshot_sha256": sha256(snapshot_path.read_bytes()).hexdigest(),
        "pages": rows,
    })
    return SgWikiMapDataCollectionResult(
        root,
        snapshot_path,
        manifest_path,
        tuple(rows),
        tuple(no_map_names),
    )


__all__ = [
    "SgWikiMapDataCollectionResult",
    "SgWikiMapDataError",
    "collect_sgwiki_mapdata",
    "discover_embedded_map",
    "parse_mapdata_response",
]
