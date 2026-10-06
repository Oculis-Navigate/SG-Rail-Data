"""Immutable collection and deterministic parsing of SgWiki station pages."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
import time
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .. import __version__
from ..progress import ProgressCallback
from ..station_catalog import StationCatalog, station_slug
from ..storage import OutputExistsError, atomic_write_bytes, atomic_write_json
from .sgwiki_parser import SgWikiSourceError, page_url, parse_response


MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpGet(Protocol):
    def __call__(
        self, url: str, *, headers: Mapping[str, str], timeout: float
    ) -> HttpResponse: ...


@dataclass(frozen=True, slots=True)
class SgWikiCollectionResult:
    output_directory: Path
    snapshot_path: Path
    manifest_path: Path
    stations: tuple[dict[str, object], ...]
    partial_station_names: tuple[str, ...]


def _read_bounded(stream: object, *, context: str) -> bytes:
    body = stream.read(MAX_RESPONSE_BYTES + 1)  # type: ignore[attr-defined]
    if len(body) > MAX_RESPONSE_BYTES:
        raise SgWikiSourceError(f"{context}: response exceeds {MAX_RESPONSE_BYTES} bytes")
    return body


def urllib_http_get(
    url: str, *, headers: Mapping[str, str], timeout: float
) -> HttpResponse:
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return HttpResponse(
                status=int(response.status),
                headers={str(key): str(value) for key, value in response.headers.items()},
                body=_read_bounded(response, context=url),
            )
    except HTTPError as error:
        return HttpResponse(
            status=int(error.code),
            headers={str(key): str(value) for key, value in error.headers.items()},
            body=_read_bounded(error, context=url),
        )
    except (URLError, TimeoutError, OSError) as error:
        raise SgWikiSourceError(f"SgWiki transport failed: {type(error).__name__}: {error}") from error


def _header(headers: Mapping[str, str], name: str) -> str | None:
    values = [value for key, value in headers.items() if key.casefold() == name.casefold()]
    if len(values) > 1:
        raise SgWikiSourceError(f"SgWiki response has repeated {name} headers")
    return values[0] if values else None


def _api_url(endpoint: str, title: str) -> str:
    parts = urlsplit(endpoint)
    if parts.scheme != "https" or parts.hostname != "sgwiki.miraheze.org":
        raise SgWikiSourceError("SgWiki API endpoint must use https://sgwiki.miraheze.org")
    if parts.query or parts.fragment or parts.username or parts.password or parts.port not in (None, 443):
        raise SgWikiSourceError("SgWiki API endpoint must not contain query, fragment or credentials")
    query = urlencode({
        "action": "parse",
        "page": title,
        "prop": "text|revid",
        "format": "json",
        "formatversion": 2,
        "redirects": 1,
    })
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


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


def collect_sgwiki(
    *,
    endpoint: str,
    catalog: StationCatalog,
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
) -> SgWikiCollectionResult:
    """Collect every configured station page and stop at the first unknown format."""

    if type(timeout_seconds) is not float or timeout_seconds <= 0:
        raise SgWikiSourceError("timeout_seconds must be a positive float")
    if type(max_attempts) is not int or type(max_attempts) is bool or max_attempts < 1:
        raise SgWikiSourceError("max_attempts must be a positive integer")
    for name, value in (
        ("initial_backoff_seconds", initial_backoff_seconds),
        ("maximum_backoff_seconds", maximum_backoff_seconds),
        ("minimum_interval_seconds", minimum_interval_seconds),
    ):
        if type(value) is not float or not math.isfinite(value) or value < 0:
            raise SgWikiSourceError(f"{name} must be a non-negative finite float")
    if maximum_backoff_seconds < initial_backoff_seconds:
        raise SgWikiSourceError("maximum_backoff_seconds must be at least initial_backoff_seconds")

    root = Path(output_directory)
    try:
        root.mkdir(parents=True)
    except FileExistsError as error:
        raise OutputExistsError(f"Refusing to reuse existing SgWiki directory: {root}") from error
    responses = root / "responses"
    rendered = root / "rendered"
    failures = root / "failures"
    responses.mkdir()
    rendered.mkdir()
    failures.mkdir()

    headers = {
        "Accept": "application/json",
        "User-Agent": f"mrt-wayfinding/{__version__} (Singapore rail wayfinding dataset)",
    }
    records: list[dict[str, object]] = []
    evidence: list[dict[str, object]] = []
    partial: list[str] = []
    previous_started: float | None = None
    for station_index, station in enumerate(catalog.stations):
        if progress is not None:
            progress(
                "SgWiki stations",
                station_index,
                len(catalog.stations),
                station.name,
            )
        slug = station_slug(station.name)
        url = _api_url(endpoint, station.sgwiki_title)
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
                failure_name = f"{station_index:03d}-{slug}-attempt-{attempt:02d}-status-{response.status}.body"
                atomic_write_bytes(failures / failure_name, response.body)
                retryable = response.status in _RETRYABLE_STATUSES
            else:
                retryable = True
            if not retryable or attempt == max_attempts:
                if response is not None:
                    raise SgWikiSourceError(
                        f"{station.name}: HTTP {response.status} after {attempt} attempt(s); "
                        f"response retained in failures/{failure_name}"
                    )
                assert last_error is not None
                raise SgWikiSourceError(
                    f"{station.name}: transport failed after {attempt} attempt(s): {last_error}"
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
            failure_name = f"{station_index:03d}-{slug}-unexpected-content-type.body"
            atomic_write_bytes(failures / failure_name, response.body)
            raise SgWikiSourceError(
                f"{station.name}: expected application/json, got {content_type!r}; "
                f"response retained in failures/{failure_name}"
            )
        response_name = f"{station_index:03d}-{slug}.json"
        atomic_write_bytes(responses / response_name, response.body)
        parsed, html = parse_response(
            response.body,
            {"name": station.name, "codes": list(station.codes)},
            source="sgwiki",
        )
        html_name = f"{station_index:03d}-{slug}.html"
        atomic_write_bytes(rendered / html_name, html.encode("utf-8"))
        parsed.update({
            "url": page_url("sgwiki", station.sgwiki_title),
            "api_url": url,
            "revision_url": f"https://sgwiki.miraheze.org/index.php?oldid={parsed['revision_id']}",
        })
        if parsed["status"] == "partial":
            partial.append(station.name)
        records.append({
            "name": station.name,
            "codes": list(station.codes),
            "catalog_status": station.catalog_status,
            "sgwiki_title": station.sgwiki_title,
            "source": parsed,
        })
        evidence.append({
            "station": station.name,
            "response_file": f"responses/{response_name}",
            "response_sha256": sha256(response.body).hexdigest(),
            "rendered_file": f"rendered/{html_name}",
            "rendered_sha256": sha256(html.encode("utf-8")).hexdigest(),
            "page_id": parsed["page_id"],
            "revision_id": parsed["revision_id"],
            "status": parsed["status"],
        })
        if progress is not None:
            progress(
                "SgWiki stations",
                station_index + 1,
                len(catalog.stations),
                station.name,
            )

    snapshot_path = atomic_write_json(root / "snapshot.json", {
        "schema_version": 1,
        "kind": "sgwiki_station_snapshot",
        "stations": records,
    })
    manifest_path = atomic_write_json(root / "collection.json", {
        "schema_version": 1,
        "kind": "sgwiki_station_collection",
        "endpoint": endpoint,
        "station_count": len(records),
        "complete_station_count": len(records) - len(partial),
        "partial_station_count": len(partial),
        "partial_station_names": partial,
        "snapshot_sha256": sha256(snapshot_path.read_bytes()).hexdigest(),
        "pages": evidence,
    })
    return SgWikiCollectionResult(
        root,
        snapshot_path,
        manifest_path,
        tuple(records),
        tuple(partial),
    )


__all__ = [
    "HttpResponse",
    "SgWikiCollectionResult",
    "SgWikiSourceError",
    "collect_sgwiki",
    "urllib_http_get",
]
