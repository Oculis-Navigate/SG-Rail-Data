"""Strict, auditable collection of LTA DataMall's BusStops dataset.

SgWiki remains authoritative for which bus-stop code is associated with an
exit.  This module downloads LTA's complete BusStops lookup and later selects
records by exact five-digit code.  It deliberately contains no distance or
nearest-neighbour matching.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import time
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .. import __version__
from ..progress import ProgressCallback
from ..storage import OutputExistsError, atomic_write_bytes, atomic_write_json, read_json


LTA_PAGE_LIMIT = 500
MAX_PAGES = 1_000
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_BUS_STOP_CODE = re.compile(r"[0-9]{5}\Z")
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_TOP_LEVEL_FIELDS = frozenset({"odata.metadata", "value"})
_RECORD_FIELDS = frozenset(
    {"BusStopCode", "RoadName", "Description", "Latitude", "Longitude"}
)


class BusStopsSourceError(RuntimeError):
    """Raised when DataMall transport or content violates the source contract."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Transport-neutral HTTP response used by the collector and its tests."""

    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpGet(Protocol):
    def __call__(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse: ...


@dataclass(frozen=True, slots=True)
class BusStop:
    """One validated LTA bus-stop lookup record."""

    code: str
    road_name: str
    description: str
    latitude: float
    longitude: float

    def as_source_dict(self) -> dict[str, object]:
        """Return the stable field names used by the LTA source contract."""

        return {
            "BusStopCode": self.code,
            "Description": self.description,
            "Latitude": self.latitude,
            "Longitude": self.longitude,
            "RoadName": self.road_name,
        }


@dataclass(frozen=True, slots=True)
class PageEvidence:
    skip: int
    record_count: int
    relative_path: str
    sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "record_count": self.record_count,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "skip": self.skip,
        }


@dataclass(frozen=True, slots=True)
class BusStopsCollectionResult:
    """Completed immutable snapshot and its normalized records."""

    output_directory: Path
    snapshot_path: Path
    manifest_path: Path
    records: tuple[BusStop, ...]
    pages: tuple[PageEvidence, ...]

    @property
    def record_count(self) -> int:
        return len(self.records)

    @property
    def page_count(self) -> int:
        return len(self.pages)


def validate_bus_stop_code(value: object, context: str) -> str:
    if type(value) is not str:
        raise BusStopsSourceError(f"{context}: expected string, got {type(value).__name__}")
    if not _BUS_STOP_CODE.fullmatch(value):
        raise BusStopsSourceError(f"{context}: expected exactly five ASCII digits")
    return value


def _exact_string(value: object, context: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise BusStopsSourceError(f"{context}: expected string, got {type(value).__name__}")
    if not allow_empty and not value:
        raise BusStopsSourceError(f"{context}: must not be empty")
    if value != value.strip():
        raise BusStopsSourceError(f"{context}: must not have surrounding whitespace")
    return value


def _coordinate(value: object, context: str, *, minimum: float, maximum: float) -> float:
    if type(value) not in (int, float):
        raise BusStopsSourceError(f"{context}: expected JSON number, got {type(value).__name__}")
    result = float(value)
    if not math.isfinite(result):
        raise BusStopsSourceError(f"{context}: must be finite")
    if not minimum <= result <= maximum:
        raise BusStopsSourceError(
            f"{context}: {result} is outside the valid range {minimum}..{maximum}"
        )
    return result


def _require_exact_fields(
    value: object,
    expected: frozenset[str],
    context: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise BusStopsSourceError(f"{context}: expected object, got {type(value).__name__}")
    non_string = [key for key in value if type(key) is not str]
    if non_string:
        raise BusStopsSourceError(f"{context}: field names must be strings")
    actual = set(value)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        raise BusStopsSourceError(
            f"{context}: unexpected fields; missing={missing}, extra={extra}"
        )
    return value


def _parse_page(body: bytes, *, skip: int) -> tuple[str, tuple[BusStop, ...]]:
    context = f"LTA BusStops response at $skip={skip}"
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BusStopsSourceError(f"{context}: response is not UTF-8") from error
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise BusStopsSourceError(f"{context}: response is not valid JSON: {error}") from error

    root = _require_exact_fields(document, _TOP_LEVEL_FIELDS, context)
    metadata = _exact_string(root["odata.metadata"], f"{context}.odata.metadata")
    raw_records = root["value"]
    if type(raw_records) is not list:
        raise BusStopsSourceError(
            f"{context}.value: expected array, got {type(raw_records).__name__}"
        )
    if len(raw_records) > LTA_PAGE_LIMIT:
        raise BusStopsSourceError(
            f"{context}.value: returned {len(raw_records)} records; declared limit is "
            f"{LTA_PAGE_LIMIT}; review the pagination contract"
        )

    records: list[BusStop] = []
    for index, raw_record in enumerate(raw_records):
        item_context = f"{context}.value[{index}]"
        record = _require_exact_fields(raw_record, _RECORD_FIELDS, item_context)
        records.append(
            BusStop(
                code=validate_bus_stop_code(
                    record["BusStopCode"], f"{item_context}.BusStopCode"
                ),
                road_name=_exact_string(record["RoadName"], f"{item_context}.RoadName"),
                description=_exact_string(
                    record["Description"],
                    f"{item_context}.Description",
                    allow_empty=True,
                ),
                latitude=_coordinate(
                    record["Latitude"],
                    f"{item_context}.Latitude",
                    minimum=-90.0,
                    maximum=90.0,
                ),
                longitude=_coordinate(
                    record["Longitude"],
                    f"{item_context}.Longitude",
                    minimum=-180.0,
                    maximum=180.0,
                ),
            )
        )
    return metadata, tuple(records)


def _page_url(endpoint: str, skip: int) -> str:
    parts = urlsplit(endpoint)
    if parts.scheme != "https" or not parts.hostname:
        raise BusStopsSourceError("LTA BusStops endpoint must be an absolute HTTPS URL")
    if parts.username is not None or parts.password is not None:
        raise BusStopsSourceError("LTA BusStops endpoint must not contain credentials")
    if parts.fragment:
        raise BusStopsSourceError("LTA BusStops endpoint must not contain a fragment")
    query = parse_qsl(parts.query, keep_blank_values=True)
    if any(name == "$skip" for name, _value in query):
        raise BusStopsSourceError("LTA BusStops endpoint must not contain a preconfigured $skip")
    query.append(("$skip", str(skip)))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query, safe="$"), "")
    )


def _read_bounded(stream: object, *, context: str) -> bytes:
    body = stream.read(MAX_RESPONSE_BYTES + 1)  # type: ignore[attr-defined]
    if len(body) > MAX_RESPONSE_BYTES:
        raise BusStopsSourceError(
            f"{context}: response exceeds {MAX_RESPONSE_BYTES} byte safety limit"
        )
    return body


def urllib_http_get(
    url: str,
    *,
    headers: Mapping[str, str],
    timeout: float,
) -> HttpResponse:
    """Fetch one response while retaining HTTP error bodies for evidence."""

    request = Request(url, headers=dict(headers), method="GET")
    try:
        # The caller supplies the strictly allowlisted URL from pipeline.toml.
        with urlopen(request, timeout=timeout) as response:
            body = _read_bounded(response, context=url)
            return HttpResponse(
                status=int(response.status),
                headers=dict(response.headers.items()),
                body=body,
            )
    except HTTPError as error:
        body = _read_bounded(error, context=url)
        return HttpResponse(
            status=int(error.code),
            headers=dict(error.headers.items()) if error.headers is not None else {},
            body=body,
        )
    except (URLError, TimeoutError, OSError) as error:
        raise BusStopsSourceError(f"GET {url}: transport failure: {error}") from error


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return value
    return None


def _retry_delay(
    response: HttpResponse | None,
    *,
    attempt: int,
    initial_backoff_seconds: float,
    maximum_backoff_seconds: float,
) -> float:
    exponential = min(
        initial_backoff_seconds * (2 ** (attempt - 1)),
        maximum_backoff_seconds,
    )
    if response is None:
        return exponential
    raw = _header(response.headers, "Retry-After")
    if raw is None or not raw.isdigit():
        return exponential
    return min(max(exponential, float(raw)), maximum_backoff_seconds)


def _create_output_directory(path: str | Path) -> tuple[Path, Path, Path]:
    root = Path(path)
    try:
        root.mkdir(parents=True)
    except FileExistsError as error:
        raise OutputExistsError(
            f"Refusing to reuse existing BusStops collection directory: {root}"
        ) from error
    pages = root / "pages"
    failures = root / "failures"
    pages.mkdir()
    failures.mkdir()
    return root, pages, failures


def collect_lta_bus_stops(
    *,
    endpoint: str,
    account_key: str,
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
) -> BusStopsCollectionResult:
    """Download every BusStops page and commit an immutable normalized snapshot.

    Pagination advances by the number of records actually returned and ends only
    on an empty page. This remains correct if LTA lowers its current 500-record
    page size. A larger page fails loudly so the declared contract can be reviewed.
    """

    if type(account_key) is not str or not account_key:
        raise BusStopsSourceError("LTA AccountKey must be a non-empty string")
    if type(timeout_seconds) is not float or timeout_seconds <= 0:
        raise BusStopsSourceError("timeout_seconds must be a positive float")
    if type(max_attempts) is not int or type(max_attempts) is bool or max_attempts < 1:
        raise BusStopsSourceError("max_attempts must be a positive integer")
    for name, value in (
        ("initial_backoff_seconds", initial_backoff_seconds),
        ("maximum_backoff_seconds", maximum_backoff_seconds),
        ("minimum_interval_seconds", minimum_interval_seconds),
    ):
        if type(value) is not float or not math.isfinite(value) or value < 0:
            raise BusStopsSourceError(f"{name} must be a non-negative finite float")
    if maximum_backoff_seconds < initial_backoff_seconds:
        raise BusStopsSourceError(
            "maximum_backoff_seconds must be at least initial_backoff_seconds"
        )

    root, pages_directory, failures_directory = _create_output_directory(output_directory)
    headers = {
        "Accept": "application/json",
        "AccountKey": account_key,
        "User-Agent": f"mrt-wayfinding/{__version__}",
    }
    records_by_code: dict[str, BusStop] = {}
    page_evidence: list[PageEvidence] = []
    metadata: str | None = None
    skip = 0
    previous_request_started: float | None = None

    for _page_number in range(MAX_PAGES):
        if progress is not None:
            progress(
                "LTA BusStops",
                len(page_evidence),
                None,
                f"requesting records from offset {skip}",
            )
        url = _page_url(endpoint, skip)
        response: HttpResponse | None = None
        last_transport_error: BusStopsSourceError | None = None
        for attempt in range(1, max_attempts + 1):
            if previous_request_started is not None:
                interval_remaining = (
                    minimum_interval_seconds - (monotonic() - previous_request_started)
                )
                if interval_remaining > 0:
                    sleep(interval_remaining)
            previous_request_started = monotonic()
            try:
                response = http_get(url, headers=headers, timeout=timeout_seconds)
                last_transport_error = None
            except BusStopsSourceError as error:
                response = None
                last_transport_error = error

            if response is not None and response.status == 200:
                break

            if response is not None:
                failure_name = (
                    f"skip-{skip:06d}-attempt-{attempt:02d}-status-{response.status}.body"
                )
                atomic_write_bytes(failures_directory / failure_name, response.body)
                retryable = response.status in _RETRYABLE_STATUSES
            else:
                retryable = True

            if not retryable or attempt == max_attempts:
                if response is not None:
                    raise BusStopsSourceError(
                        f"LTA BusStops $skip={skip}: HTTP {response.status} after "
                        f"{attempt} attempt(s); response retained in failures/{failure_name}"
                    )
                assert last_transport_error is not None
                raise BusStopsSourceError(
                    f"LTA BusStops $skip={skip}: transport failed after {attempt} "
                    f"attempt(s): {last_transport_error}"
                ) from last_transport_error

            sleep(
                _retry_delay(
                    response,
                    attempt=attempt,
                    initial_backoff_seconds=initial_backoff_seconds,
                    maximum_backoff_seconds=maximum_backoff_seconds,
                )
            )

        assert response is not None and response.status == 200
        content_type = _header(response.headers, "Content-Type")
        if (
            content_type is None
            or content_type.split(";", 1)[0].strip().casefold() != "application/json"
        ):
            failure_name = f"skip-{skip:06d}-unexpected-content-type.body"
            atomic_write_bytes(failures_directory / failure_name, response.body)
            raise BusStopsSourceError(
                f"LTA BusStops $skip={skip}: expected application/json Content-Type, "
                f"got {content_type!r}; response retained in failures/{failure_name}"
            )

        page_name = f"skip-{skip:06d}.json"
        page_path = pages_directory / page_name
        atomic_write_bytes(page_path, response.body)
        page_metadata, page_records = _parse_page(response.body, skip=skip)
        if metadata is None:
            metadata = page_metadata
        elif page_metadata != metadata:
            raise BusStopsSourceError(
                f"LTA BusStops $skip={skip}: odata.metadata changed within one collection"
            )

        for record in page_records:
            previous = records_by_code.get(record.code)
            if previous is not None:
                raise BusStopsSourceError(
                    f"LTA BusStops $skip={skip}: duplicate BusStopCode {record.code}; "
                    "exact joins would be ambiguous"
                )
            records_by_code[record.code] = record

        page_evidence.append(
            PageEvidence(
                skip=skip,
                record_count=len(page_records),
                relative_path=f"pages/{page_name}",
                sha256=sha256(response.body).hexdigest(),
            )
        )
        if progress is not None:
            progress(
                "LTA BusStops",
                len(page_evidence),
                None,
                f"{len(records_by_code)} bus stops",
            )
        if not page_records:
            break
        skip += len(page_records)
    else:
        raise BusStopsSourceError(
            f"LTA BusStops pagination exceeded {MAX_PAGES} pages; review required"
        )

    if not records_by_code:
        raise BusStopsSourceError("LTA BusStops returned no records")

    if progress is not None:
        progress(
            "LTA BusStops",
            len(page_evidence),
            len(page_evidence),
            f"{len(records_by_code)} bus stops",
        )

    records = tuple(records_by_code[code] for code in sorted(records_by_code))
    snapshot_path = atomic_write_json(
        root / "snapshot.json",
        {
            "bus_stops": [record.as_source_dict() for record in records],
            "kind": "lta_bus_stops_snapshot",
            "schema_version": 1,
        },
    )
    manifest_path = atomic_write_json(
        root / "collection.json",
        {
            "endpoint": endpoint,
            "kind": "lta_bus_stops_collection",
            "page_count": len(page_evidence),
            "pages": [page.as_dict() for page in page_evidence],
            "record_count": len(records),
            "schema_version": 1,
            "snapshot_sha256": sha256(snapshot_path.read_bytes()).hexdigest(),
        },
    )
    return BusStopsCollectionResult(
        output_directory=root,
        snapshot_path=snapshot_path,
        manifest_path=manifest_path,
        records=records,
        pages=tuple(page_evidence),
    )


def select_required_bus_stops(
    records: Sequence[BusStop],
    required_codes: Iterable[str],
) -> tuple[BusStop, ...]:
    """Resolve SgWiki codes against LTA records using equality only."""

    by_code: dict[str, BusStop] = {}
    for index, record in enumerate(records):
        if type(record) is not BusStop:
            raise BusStopsSourceError(
                f"records[{index}]: expected BusStop, got {type(record).__name__}"
            )
        if record.code in by_code:
            raise BusStopsSourceError(
                f"records[{index}]: duplicate BusStopCode {record.code}; exact join is ambiguous"
            )
        by_code[record.code] = record

    wanted: set[str] = set()
    for index, code in enumerate(required_codes):
        wanted.add(validate_bus_stop_code(code, f"required_codes[{index}]"))
    if not wanted:
        raise BusStopsSourceError("required_codes: at least one SgWiki bus-stop code is required")

    missing = sorted(wanted - by_code.keys())
    if missing:
        raise BusStopsSourceError(
            f"SgWiki bus-stop codes missing from LTA BusStops: {missing}"
        )
    return tuple(by_code[code] for code in sorted(wanted))


def load_lta_bus_stops_snapshot(path: str | Path) -> tuple[BusStop, ...]:
    """Load a previously collected native snapshot through the same strict record parser."""

    source = Path(path)
    document = read_json(source)
    root = _require_exact_fields(
        document, frozenset({"schema_version", "kind", "bus_stops"}), "LTA BusStops snapshot"
    )
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise BusStopsSourceError("LTA BusStops snapshot.schema_version: expected integer 1")
    if root["kind"] != "lta_bus_stops_snapshot":
        raise BusStopsSourceError("LTA BusStops snapshot.kind: unsupported value")
    rows = root["bus_stops"]
    if type(rows) is not list or not rows:
        raise BusStopsSourceError("LTA BusStops snapshot.bus_stops: expected nonempty array")
    records: list[BusStop] = []
    seen: set[str] = set()
    for index, value in enumerate(rows):
        context = f"LTA BusStops snapshot.bus_stops[{index}]"
        row = _require_exact_fields(value, _RECORD_FIELDS, context)
        record = BusStop(
            code=validate_bus_stop_code(row["BusStopCode"], f"{context}.BusStopCode"),
            road_name=_exact_string(row["RoadName"], f"{context}.RoadName"),
            description=_exact_string(
                row["Description"], f"{context}.Description", allow_empty=True
            ),
            latitude=_coordinate(
                row["Latitude"], f"{context}.Latitude", minimum=-90.0, maximum=90.0
            ),
            longitude=_coordinate(
                row["Longitude"], f"{context}.Longitude", minimum=-180.0, maximum=180.0
            ),
        )
        if record.code in seen:
            raise BusStopsSourceError(
                f"{context}.BusStopCode: duplicate code {record.code}"
            )
        seen.add(record.code)
        records.append(record)
    return tuple(records)


__all__ = [
    "BusStop",
    "BusStopsCollectionResult",
    "BusStopsSourceError",
    "HttpResponse",
    "collect_lta_bus_stops",
    "load_lta_bus_stops_snapshot",
    "select_required_bus_stops",
    "urllib_http_get",
]
