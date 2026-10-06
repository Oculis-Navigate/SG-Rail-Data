"""Strict loading of the reviewed station-to-SgWiki page registry."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

from .contracts import ContractError, exact_str, fail, require_fields, string_list


STATION_CODE = re.compile(r"(?:[A-Z]{1,3}[0-9]+|STC|PTC)\Z")


@dataclass(frozen=True, slots=True)
class CatalogStation:
    name: str
    codes: tuple[str, ...]
    catalog_status: str
    sgwiki_title: str


@dataclass(frozen=True, slots=True)
class StationCatalog:
    path: Path
    stations: tuple[CatalogStation, ...]


def station_key(name: str) -> str:
    """Return the exact canonical comparison key used for source joins."""

    normalized = " ".join(name.upper().split())
    return re.sub(r"(?: (?:MRT/LRT|MRT|LRT))? STATION$", "", normalized)


def station_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", station_key(name).lower()).strip("-")


def load_station_catalog(path: str | Path, *, expected_count: int) -> StationCatalog:
    source = Path(path).resolve()
    raw = source.read_bytes()
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"station catalog: invalid JSON in {source}: {error}") from error
    root = require_fields(document, {"schema_version", "source", "stations"}, "station catalog")
    if root["schema_version"] != 1 or type(root["schema_version"]) is not int:
        fail("station catalog.schema_version", "expected integer 1")
    provenance = require_fields(
        root["source"], {"kind", "derived_from", "reviewed_on"}, "station catalog.source"
    )
    if exact_str(provenance["kind"], "station catalog.source.kind") != "reviewed_station_page_registry":
        fail("station catalog.source.kind", "unsupported registry kind")
    exact_str(provenance["derived_from"], "station catalog.source.derived_from")
    reviewed_on = exact_str(provenance["reviewed_on"], "station catalog.source.reviewed_on")
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", reviewed_on) is None:
        fail("station catalog.source.reviewed_on", "expected YYYY-MM-DD")
    if type(root["stations"]) is not list:
        fail("station catalog.stations", "expected array")
    if len(root["stations"]) != expected_count:
        fail(
            "station catalog.stations",
            f"expected exactly {expected_count} records, got {len(root['stations'])}",
        )

    stations: list[CatalogStation] = []
    names: set[str] = set()
    slugs: set[str] = set()
    assigned_codes: set[str] = set()
    titles: set[str] = set()
    for index, value in enumerate(root["stations"]):
        context = f"station catalog.stations[{index}]"
        row = require_fields(
            value, {"name", "codes", "catalog_status", "sgwiki_title"}, context
        )
        name = exact_str(row["name"], f"{context}.name")
        key = station_key(name)
        slug = station_slug(name)
        if key in names or slug in slugs:
            fail(context, "duplicate station identity or slug")
        names.add(key)
        slugs.add(slug)
        codes = string_list(row["codes"], f"{context}.codes", nonempty=True)
        invalid = [code for code in codes if STATION_CODE.fullmatch(code) is None]
        if invalid:
            fail(f"{context}.codes", f"invalid station codes {invalid}")
        repeated_codes = sorted(set(codes) & assigned_codes)
        if repeated_codes:
            fail(f"{context}.codes", f"codes assigned to multiple stations {repeated_codes}")
        assigned_codes.update(codes)
        catalog_status = exact_str(row["catalog_status"], f"{context}.catalog_status")
        if catalog_status not in {"v1_baseline", "operator_addition_requires_review"}:
            fail(f"{context}.catalog_status", f"unsupported value {catalog_status!r}")
        title = exact_str(row["sgwiki_title"], f"{context}.sgwiki_title")
        if title in titles or not title.endswith(" station"):
            fail(f"{context}.sgwiki_title", "must be a unique SgWiki station title")
        titles.add(title)
        if station_key(title) != key:
            fail(f"{context}.sgwiki_title", "title does not identify the configured station")
        stations.append(CatalogStation(name, codes, catalog_status, title))
    return StationCatalog(source, tuple(stations))


__all__ = [
    "CatalogStation",
    "STATION_CODE",
    "StationCatalog",
    "load_station_catalog",
    "station_key",
    "station_slug",
]
